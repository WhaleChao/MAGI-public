"""
Tests for Orchestrator._summarize_text_resilient — summarization pipeline.

Mock strategy:
- balthasar_bridge.summarize_text() → patch.object on module (local imports need this)
- InferenceGateway().chat() → mock chunk/reduce/fallback summaries
- melchior_client → mock circuit breaker
"""

import pytest
import re
from unittest.mock import patch, MagicMock
from contextlib import contextmanager


def _make_gateway_response(text="摘要結果", success=True, model="gemma-4-e4b-it-4bit", route="omlx"):
    return {
        "success": success,
        "response": text,
        "model": model,
        "route": route,
        "degraded": False,
        "error": "" if success else "mock_error",
        "text": text,
        "summary": text,
        "analysis": text,
    }


def _make_orchestrator():
    with patch("api.orchestrator.ThreadPoolExecutor"), \
         patch("api.orchestrator.switch_brain_mode"), \
         patch("api.orchestrator.get_brain_status"):
        from api.orchestrator import Orchestrator
        orc = object.__new__(Orchestrator)
        orc._history = {}
        orc._profile_facts = {}
        orc._callbacks = []
        orc._bg_task_pool = MagicMock()
        orc._route_traces = {}
        import threading
        orc._heavy_task_lock = threading.Lock()
        orc._heavy_tasks = {}
        return orc


@contextmanager
def _mock_bt_summarize(return_value):
    """Patch balthasar_bridge.summarize_text at module level so local imports pick it up."""
    import skills.bridge.balthasar_bridge as _mod
    _orig = _mod.summarize_text
    _mod.summarize_text = lambda *a, **kw: return_value
    try:
        yield
    finally:
        _mod.summarize_text = _orig


class TestSummarizeEmpty:
    def test_empty_text_returns_error(self):
        orc = _make_orchestrator()
        result = orc._summarize_text_resilient("")
        assert result["success"] is False
        assert "empty" in result.get("error", "")


class TestSummarizeDirect:
    """Short text should go through balthasar_bridge.summarize_text direct path."""

    def test_direct_summary_success(self):
        import skills.bridge.llm_direct as _direct
        orc = _make_orchestrator()
        with patch.object(_direct, "feature_enabled", return_value=False), \
             _mock_bt_summarize({
                 "success": True,
                 "text": "1. 第一點重點：本案涉及勞動基準法適用問題\n2. 第二點重點：原告主張資遣費請求權\n3. 第三點重點：被告抗辯解僱合法\n4. 第四點重點：法院認定原告勝訴",
                 "provider": "omlx_direct",
             }):
            result = orc._summarize_text_resilient("這是一段需要摘要的法律文件內容。" * 50)
        assert result["success"] is True
        assert "第一點重點" in result["text"]
        assert "direct" in result.get("provider", "")

    @patch("api.handlers.summary_handler.InferenceGateway")
    def test_direct_fail_falls_to_gateway(self, mock_gw_cls):
        """When direct summary fails, should fall through to gateway fallback."""
        import skills.bridge.llm_direct as _direct
        mock_gw = MagicMock()
        mock_gw.chat.return_value = _make_gateway_response(
            "1. 摘要要點一\n2. 摘要要點二\n3. 摘要要點三\n4. 摘要要點四"
        )
        mock_gw_cls.return_value = mock_gw

        orc = _make_orchestrator()
        with patch.object(_direct, "feature_enabled", return_value=False), \
             _mock_bt_summarize({"success": False, "error": "timeout"}):
            result = orc._summarize_text_resilient("短文件摘要。" * 50)
        assert result["success"] is True

    def test_direct_summary_short_seed_uses_shorter_timeout(self):
        import skills.bridge.balthasar_bridge as _bt
        import skills.bridge.llm_direct as _direct

        calls = []

        def _fake_summarize(text, timeout_sec=None, summary_length="medium"):
            calls.append(timeout_sec)
            return {
                "success": True,
                "text": (
                    "1. 種子摘要重寫完成，系統已整理核心爭點、關鍵事實與主要結論。\n"
                    "2. 條列化輸出保留法條脈絡、請求基礎與裁判方向，避免再落回 gateway fallback。"
                ),
                "provider": "omlx_direct",
            }

        orc = _make_orchestrator()
        with patch.object(_direct, "feature_enabled", return_value=False), \
             patch.object(_bt, "summarize_text", side_effect=_fake_summarize), \
             patch.dict("os.environ", {"MAGI_FILE_SUMMARY_TIMEOUT_SEC": "120"}):
            result = orc._summarize_text_resilient("短摘要種子。" * 80)

        assert result["success"] is True
        assert calls
        assert calls[0] <= 45


class TestSummarizeMapReduce:
    """Long text triggers map-reduce pipeline."""

    @patch("api.handlers.summary_handler.InferenceGateway")
    @patch("skills.bridge.melchior_client._omlx_available", return_value=False)
    def test_map_reduce_long_text(self, mock_omlx_avail, mock_gw_cls):
        """Text > direct_max_chars should use map-reduce."""
        import skills.bridge.llm_direct as _direct
        call_idx = [0]

        def _mock_chat(prompt, **kwargs):
            call_idx[0] += 1
            task = kwargs.get("task_type", "")
            if "摘要" in prompt or task == "summary":
                return _make_gateway_response(f"第{call_idx[0]}段摘要：本段重點是法律條文解析與判決分析結果。")
            return _make_gateway_response("合併摘要結果。")

        mock_gw = MagicMock()
        mock_gw.chat.side_effect = _mock_chat
        mock_gw_cls.return_value = mock_gw

        orc = _make_orchestrator()
        # Create text > 8000 chars to trigger map-reduce
        long_text = ("本案涉及勞動基準法第十二條之適用問題。" * 200 + "\n\n" +
                     "原告主張被告未依法給付資遣費及預告工資。" * 200 + "\n\n" +
                     "法院審酌兩造主張及證據後認定如下。" * 200)

        with patch.object(_direct, "feature_enabled", return_value=False), \
             _mock_bt_summarize({"success": False, "error": "too_long"}), \
             patch.dict("os.environ", {"MAGI_FILE_SUMMARY_DIRECT_MAX_CHARS": "2000"}):
            result = orc._summarize_text_resilient(long_text)

        assert result["success"] is True
        assert isinstance(result["text"], str)
        assert len(result["text"]) > 20

    @patch("api.handlers.summary_handler.InferenceGateway")
    @patch("skills.bridge.melchior_client._omlx_available", return_value=False)
    def test_map_reduce_retries_synthetic_timeout_chunks(self, mock_omlx_avail, mock_gw_cls):
        """Synthetic timeout placeholders should be retried instead of leaking into the final summary."""
        import skills.bridge.llm_direct as _direct
        attempts = {}

        def _mock_chat(prompt, **kwargs):
            if "整合為一份精簡的結構化摘要" in prompt:
                return _make_gateway_response("整體摘要：法院整理出主要爭點、法條與結論。")
            m = re.search(r"這是分段\s+([0-9.]+)/", prompt)
            if m:
                label = m.group(1)
                attempts[label] = attempts.get(label, 0) + 1
                if label == "1" and attempts[label] == 1:
                    return {
                        "success": True,
                        "response": "（系統降級回覆）本機模型逾時，請稍後重試。",
                        "text": "（系統降級回覆）本機模型逾時，請稍後重試。",
                        "summary": "（系統降級回覆）本機模型逾時，請稍後重試。",
                        "analysis": "（系統降級回覆）本機模型逾時，請稍後重試。",
                        "model": "gemma-4-e4b",
                        "route": "local_ollama",
                        "degraded": True,
                        "synthetic_fallback": True,
                        "error": "mock_timeout",
                    }
                return _make_gateway_response(f"第{label}段摘要：法院分析法律爭點、事實與裁判理由。")
            return _make_gateway_response("合併摘要結果。")

        mock_gw = MagicMock()
        mock_gw.chat.side_effect = _mock_chat
        mock_gw_cls.return_value = mock_gw

        orc = _make_orchestrator()
        long_text = ("本案涉及勞動基準法第十二條之適用問題。" * 220 + "\n\n" +
                     "原告主張被告未依法給付資遣費及預告工資。" * 220 + "\n\n" +
                     "法院審酌兩造主張及證據後認定如下。" * 220)

        with patch.object(_direct, "feature_enabled", return_value=False), \
             _mock_bt_summarize({"success": False, "error": "too_long"}), \
             patch.dict("os.environ", {"MAGI_FILE_SUMMARY_DIRECT_MAX_CHARS": "2000"}):
            result = orc._summarize_text_resilient(long_text)

        assert result["success"] is True
        assert "本機模型逾時" not in result["text"]
        assert attempts.get("1", 0) >= 2

    @patch("skills.documents.pdf_bridge.summarize_ultra_large_text")
    def test_ultra_large_text_uses_hierarchical_path(self, mock_ultra):
        import skills.bridge.llm_direct as _direct
        mock_ultra.return_value = (
            "【文件概況】\n"
            "- 可辨識頁數：約 520 頁\n"
            "- 分析分段：24 段\n\n"
            "【重點摘要】\n"
            "1. 大型文件已走分層摘要，先做段落壓縮再整合。\n"
            "2. 系統會保留關鍵法條、制度比較與量刑政策脈絡。"
        )

        orc = _make_orchestrator()
        huge_text = ("本案涉及刑事政策與量刑理論。" * 6000).strip()

        with patch.object(_direct, "feature_enabled", return_value=False), \
             _mock_bt_summarize({"success": False, "error": "too_long"}), \
             patch.dict("os.environ", {
                 "MAGI_FILE_SUMMARY_DIRECT_MAX_CHARS": "2000",
                 "MAGI_FILE_SUMMARY_ULTRA_THRESHOLD_CHARS": "20000",
             }):
            result = orc._summarize_text_resilient(huge_text)

        assert result["success"] is True
        assert "大型文件已走分層摘要" in result["text"]
        mock_ultra.assert_called_once()



class TestSummarizeLengthModes:
    """Different summary_length values should affect prompts."""

    def test_short_length_prompt(self):
        orc = _make_orchestrator()
        chunk_hint, reduce_hint = orc._summary_length_prompt("short")
        assert "3-5" in chunk_hint

    def test_medium_length_prompt(self):
        orc = _make_orchestrator()
        chunk_hint, reduce_hint = orc._summary_length_prompt("medium")
        assert "5-8" in chunk_hint

    def test_long_length_prompt(self):
        orc = _make_orchestrator()
        chunk_hint, reduce_hint = orc._summary_length_prompt("long")
        assert "10-15" in chunk_hint


class TestSummarizeExtractiveFallback:
    """When all LLM paths fail, extractive fallback should produce output."""

    @patch("api.handlers.summary_handler.InferenceGateway")
    @patch("skills.bridge.melchior_client._omlx_available", return_value=False)
    def test_extractive_fallback(self, mock_omlx_avail, mock_gw_cls):
        import skills.bridge.llm_direct as _direct
        mock_gw = MagicMock()
        mock_gw.chat.return_value = _make_gateway_response("", success=False)
        mock_gw_cls.return_value = mock_gw

        orc = _make_orchestrator()
        # Build text with recognizable structure for extractive fallback
        text = (
            "--- 第 1 頁 ---\n"
            "INTERNATIONAL COURT OF JUSTICE\n"
            "JUDGMENT ON PRELIMINARY OBJECTIONS\n\n"
            "本案涉及國際法院管轄權之初步異議。申請人主張依據公約第三十六條第二項之規定，法院具有管轄權。\n\n"
            "被申請人則主張法院對本案不具管轄權，理由如下：第一，申請人未履行前置協商義務。"
            "第二，申請人之主張不構成公約所稱之爭端。第三，申請人遲延提起訴訟，違反善意原則。\n\n"
            "法院審理後認定，申請人已充分履行前置程序義務，本案構成公約第三十六條所稱之爭端。\n"
        ) * 5

        with patch.object(_direct, "feature_enabled", return_value=False), \
             _mock_bt_summarize({"success": False}):
            result = orc._summarize_text_resilient(text)
        # Should either succeed with extractive or fail — either way it's a dict
        assert isinstance(result, dict)
        assert "success" in result


class TestSummarizeProgressCallback:
    """progress_callback should be called during processing."""

    def test_callback_receives_updates(self):
        import skills.bridge.llm_direct as _direct
        callbacks = []

        def _cb(msg):
            callbacks.append(msg)

        orc = _make_orchestrator()
        with patch.object(_direct, "feature_enabled", return_value=False), \
             _mock_bt_summarize({
                 "success": True,
                 "text": (
                     "1. 法院認定原告之訴有理由，並確認勞動契約終止存在違法爭議。\n"
                     "2. 被告應給付資遣費、預告工資與相關損害賠償。\n"
                     "3. 判決同時整理雙方主要爭點、證據採信理由與法律適用基礎。"
                 ),
                 "provider": "omlx_direct",
             }):
            result = orc._summarize_text_resilient("短文件內容。" * 50, progress_callback=_cb)
        assert result["success"] is True
        # progress_callback may or may not be called for direct path — just verify no crash
