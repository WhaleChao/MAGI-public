"""Regression tests for low-hallucination orchestration paths."""

from collections import deque
from threading import Lock
from unittest.mock import MagicMock, patch


def _make_orchestrator():
    with patch("api.orchestrator.ThreadPoolExecutor"), \
         patch("api.orchestrator.switch_brain_mode"), \
         patch("api.orchestrator.get_brain_status"):
        from api.orchestrator import Orchestrator

        orc = object.__new__(Orchestrator)
        orc.user_history = {
            "u1": deque([
                {"role": "user", "content": "先前問題"},
                {"role": "assistant", "content": "先前回覆"},
                {"role": "user", "content": "新的追問"},
            ])
        }
        orc._history_summaries = {"u1": "這裡是舊摘要，提到一些決策。"}
        orc._history_summaries_lock = Lock()
        orc._HISTORY_TOKEN_BUDGET = 10000
        orc._estimate_tokens = lambda text: max(1, len(str(text)) // 2)
        orc._brain_runtime_banner = lambda: "BANNER"
        orc._call_with_timeout = MagicMock(
            return_value="⚠️ 查詢逾時（>120s），目前沒有可驗證結果。"
        )
        orc._inference_gw = MagicMock()
        return orc


def test_history_summary_is_marked_non_authoritative():
    orc = _make_orchestrator()

    history = orc._build_conversation_history("u1", limit=8)

    assert "非原文" in history
    assert "僅供延續上下文" in history
    assert "來源：模型壓縮" in history
    assert "[系統]" not in history


def test_query_timeout_uses_retry_safe_fallback():
    orc = _make_orchestrator()

    with patch.object(orc, "_call_with_timeout", return_value="⚠️ 查詢逾時（>120s），目前沒有可驗證結果。"), \
         patch.object(orc, "_brain_runtime_banner", return_value="BANNER"):
        result = orc._handle_query("u1", "請幫我查一下最新法規", platform_hint="LINE")

    assert "BANNER" in result
    assert "未驗證回覆" in result
    assert "目前沒有可驗證結果" in result
    assert orc._inference_gw.chat.call_count == 0


def test_admin_gap_interview_requires_explicit_creation_request():
    orc = _make_orchestrator()

    assert not orc._should_start_skill_interview_from_gap(
        "舉例而言，文章中第50個範例內容是什麼",
        "admin",
        intent="CHAT",
    )
    assert not orc._should_start_skill_interview_from_gap(
        "可以幫我查一下嗎",
        "admin",
        intent="QUERY",
    )
    assert orc._should_start_skill_interview_from_gap(
        "幫我建立一個技能，做一個自動同步工具",
        "admin",
        intent="CHAT",
    )


def test_record_assistant_reply_does_not_persist_long_term_chatlog_by_default(monkeypatch):
    orc = _make_orchestrator()
    orc._append_history = MagicMock()
    orc._maybe_capture_chatlog = MagicMock()
    monkeypatch.delenv("MAGI_CAPTURE_ASSISTANT_CHATLOG", raising=False)

    orc.record_assistant_reply("u1", "這是一段 assistant 回覆")

    orc._append_history.assert_called_once()
    orc._maybe_capture_chatlog.assert_not_called()


def test_record_assistant_reply_can_persist_when_explicitly_enabled(monkeypatch):
    orc = _make_orchestrator()
    orc._append_history = MagicMock()
    orc._maybe_capture_chatlog = MagicMock()
    monkeypatch.setenv("MAGI_CAPTURE_ASSISTANT_CHATLOG", "1")

    orc.record_assistant_reply("u1", "這是一段 assistant 回覆")

    orc._append_history.assert_called_once()
    orc._maybe_capture_chatlog.assert_called_once_with("u1", "unknown", "assistant", "這是一段 assistant 回覆")
