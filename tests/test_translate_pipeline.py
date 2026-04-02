"""
Tests for Orchestrator._translate_text_complete — translation pipeline.

Mock strategy:
- InferenceGateway().chat() → controlled mock responses
- urllib.request.urlopen → mock GTX fallback
- melchior_client.get_circuit_breaker_status() → closed circuit
"""

import json
import pytest
import time
from unittest.mock import patch, MagicMock


def _make_gateway_response(text="翻譯結果", success=True, model="TAIDE-12b-Chat-mlx-4bit", route="omlx"):
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
    """Create a minimal Orchestrator instance with mocked init dependencies."""
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


class TestTranslateEmpty:
    def test_empty_text_returns_error(self):
        orc = _make_orchestrator()
        result = orc._translate_text_complete("")
        assert result["success"] is False
        assert "empty" in result.get("error", "")


class TestTranslateSingleChunk:
    """Single-chunk text should go through Gateway and return translated text."""

    @patch("api.handlers.translation_handler.InferenceGateway")
    @patch("skills.bridge.melchior_client.get_circuit_breaker_status", return_value={"open": False})
    def test_single_chunk_success(self, mock_cb, mock_gw_cls):
        mock_gw = MagicMock()
        mock_gw.chat.return_value = _make_gateway_response("這是翻譯後的文字")
        mock_gw_cls.return_value = mock_gw

        orc = _make_orchestrator()
        result = orc._translate_text_complete("This is English text to translate.")

        assert result["success"] is True
        assert "翻譯後的文字" in result["text"]
        assert "| 原文 | 中文 |" in result["text"]
        assert "This is English text to translate." in result["text"]
        assert result["chunks_total"] >= 1
        assert result["chunks_failed"] == 0
        first_call = mock_gw.chat.call_args_list[0]
        assert first_call.kwargs.get("allow_synthetic_fallback") is False

    @patch("api.handlers.translation_handler.InferenceGateway")
    @patch("skills.bridge.melchior_client.get_circuit_breaker_status", return_value={"open": False})
    def test_single_chunk_all_fail_returns_original(self, mock_cb, mock_gw_cls):
        """When all engines fail, should still return with original text preserved."""
        mock_gw = MagicMock()
        mock_gw.chat.return_value = _make_gateway_response("", success=False)
        mock_gw_cls.return_value = mock_gw

        orc = _make_orchestrator()
        # Disable GTX fallback
        with patch.dict("os.environ", {"MAGI_FILE_TRANSLATE_GTX_FALLBACK": "0"}):
            result = orc._translate_text_complete("Untranslatable text.")

        # Should still return — either partial success or failure with original text
        assert isinstance(result, dict)
        assert result["chunks_total"] >= 1


class TestTranslateMultiChunk:
    """Multi-chunk text uses ThreadPoolExecutor for parallel translation."""

    @patch("api.handlers.translation_handler.InferenceGateway")
    @patch("skills.bridge.melchior_client.get_circuit_breaker_status", return_value={"open": False})
    def test_multi_chunk_reassembly(self, mock_cb, mock_gw_cls):
        """Translated chunks should be reassembled in order."""
        call_count = [0]

        def _mock_chat(prompt, **kwargs):
            call_count[0] += 1
            return _make_gateway_response(f"翻譯段落{call_count[0]}")

        mock_gw = MagicMock()
        mock_gw.chat.side_effect = _mock_chat
        mock_gw_cls.return_value = mock_gw

        orc = _make_orchestrator()
        # Create text large enough to split into multiple chunks
        long_text = ("This is paragraph one about international law. " * 60 + "\n\n" +
                     "This is paragraph two about treaty obligations. " * 60 + "\n\n" +
                     "This is paragraph three about jurisdictional matters. " * 60)

        # Disable GTX so only Gateway mock is used
        with patch.dict("os.environ", {"MAGI_FILE_TRANSLATE_GTX_FALLBACK": "0"}):
            result = orc._translate_text_complete(long_text)

        assert result["success"] is True
        assert result["chunks_total"] >= 2
        assert result["chunks_failed"] == 0
        # Gateway mock was called multiple times
        assert mock_gw.chat.call_count >= 2

    @patch("api.handlers.translation_handler.InferenceGateway")
    @patch("skills.bridge.melchior_client.get_circuit_breaker_status", return_value={"open": False})
    def test_failed_chunk_can_split_and_recover(self, mock_cb, mock_gw_cls):
        def _mock_chat(prompt, **kwargs):
            task = kwargs.get("task_type", "")
            if task == "tc_review":
                return _make_gateway_response("正確", model="taide-12b")
            if task == "translate":
                if "子段：" in prompt:
                    return _make_gateway_response("分段翻譯成功")
                return _make_gateway_response("", success=False)
            return _make_gateway_response("", success=False)

        mock_gw = MagicMock()
        mock_gw.chat.side_effect = _mock_chat
        mock_gw_cls.return_value = mock_gw

        orc = _make_orchestrator()
        long_text = "English legal text. " * 600

        with patch.dict("os.environ", {
            "MAGI_FILE_TRANSLATE_GTX_FALLBACK": "0",
            "MAGI_FILE_TRANSLATE_RETRIES": "0",
            "MAGI_FILE_TRANSLATE_SPLIT_RETRY_DEPTH": "1",
            "MAGI_FILE_TRANSLATE_SPLIT_RETRY_CHARS": "1200",
        }):
            result = orc._translate_text_complete(long_text)

        assert result["success"] is True
        assert "分段翻譯成功" in result["text"]
        assert result["chunks_failed"] == 0


class TestTranslateGTXFallback:
    """GTX (Google Translate) fallback when LLM translation fails."""

    @patch("api.handlers.translation_handler.InferenceGateway")
    @patch("skills.bridge.melchior_client.get_circuit_breaker_status", return_value={"open": True})
    @patch("urllib.request.urlopen")
    def test_gtx_fallback_on_circuit_open(self, mock_urlopen, mock_cb, mock_gw_cls):
        """When circuit breaker is open + GTX enabled, should attempt GTX."""
        # Mock GTX response
        import json
        gtx_data = json.dumps([[["GTX翻譯結果", "source text"]]]).encode("utf-8")
        mock_resp = MagicMock()
        mock_resp.read.return_value = gtx_data
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        # Gateway fails
        mock_gw = MagicMock()
        mock_gw.chat.return_value = _make_gateway_response("", success=False)
        mock_gw_cls.return_value = mock_gw

        orc = _make_orchestrator()
        with patch.dict("os.environ", {
            "MAGI_FILE_TRANSLATE_GTX_FALLBACK": "1",
            "MAGI_FILE_TRANSLATE_GTX_PRIMARY": "1",
        }):
            result = orc._translate_text_complete("English text for GTX.")

        assert isinstance(result, dict)

    @patch("api.handlers.translation_handler.InferenceGateway")
    @patch("skills.bridge.melchior_client.get_circuit_breaker_status", return_value={"open": False})
    @patch("urllib.request.urlopen")
    def test_gtx_auto_primary_for_large_cjk_to_english(self, mock_urlopen, mock_cb, mock_gw_cls):
        import json

        gtx_data = json.dumps([[["GTX English output", "來源文字"]]]).encode("utf-8")
        mock_resp = MagicMock()
        mock_resp.read.return_value = gtx_data
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        def _mock_chat(prompt, **kwargs):
            if kwargs.get("task_type") == "tc_review":
                return _make_gateway_response("正確", model="taide-12b")
            return _make_gateway_response("", success=False)

        mock_gw = MagicMock()
        mock_gw.chat.side_effect = _mock_chat
        mock_gw_cls.return_value = mock_gw

        orc = _make_orchestrator()
        long_cjk_text = "這是一段中文法律教材內容。" * 600

        with patch.dict("os.environ", {
            "MAGI_FILE_TRANSLATE_GTX_FALLBACK": "1",
            "MAGI_FILE_TRANSLATE_GTX_PRIMARY": "auto",
        }):
            result = orc._translate_text_complete(long_cjk_text, target_lang="English")

        assert result["success"] is True
        assert "GTX English output" in result["text"]
        assert mock_urlopen.called


class TestTranslateTimeoutHandling:
    """Hung translation chunks should not block the whole pipeline forever."""

    @patch("api.handlers.translation_handler.InferenceGateway")
    @patch("skills.bridge.melchior_client.get_circuit_breaker_status", return_value={"open": False})
    def test_idle_timeout_returns_without_hanging(self, mock_cb, mock_gw_cls):
        def _slow_chat(prompt, **kwargs):
            time.sleep(2.0)
            return _make_gateway_response("", success=False)

        mock_gw = MagicMock()
        mock_gw.chat.side_effect = _slow_chat
        mock_gw_cls.return_value = mock_gw

        orc = _make_orchestrator()
        started = time.monotonic()
        with patch.dict("os.environ", {
            "MAGI_FILE_TRANSLATE_GTX_FALLBACK": "0",
            "MAGI_FILE_TRANSLATE_RETRIES": "0",
            "MAGI_FILE_TRANSLATE_IDLE_TIMEOUT_SEC": "1",
        }):
            result = orc._translate_text_complete("English legal text. " * 300)
        elapsed = time.monotonic() - started

        assert elapsed < 1.8
        assert isinstance(result, dict)
        assert result["chunks_failed"] >= 1

    @patch("api.handlers.translation_handler.InferenceGateway")
    @patch("skills.bridge.melchior_client.get_circuit_breaker_status", return_value={"open": False})
    def test_checkpoint_resume_skips_completed_work(self, mock_cb, mock_gw_cls, tmp_path):
        call_count = [0]

        def _mock_chat(prompt, **kwargs):
            task = kwargs.get("task_type", "")
            if task == "tc_review":
                return _make_gateway_response("正確", model="taide-12b")
            call_count[0] += 1
            return _make_gateway_response(f"翻譯段落{call_count[0]}")

        mock_gw = MagicMock()
        mock_gw.chat.side_effect = _mock_chat
        mock_gw_cls.return_value = mock_gw

        orc = _make_orchestrator()
        long_text = (
            ("English legal text about sentencing policy. " * 140)
            + "\n\n"
            + ("Another paragraph about probation and parole. " * 140)
        )

        env = {
            "MAGI_DOC_RUN_ROOT": str(tmp_path),
            "MAGI_FILE_TRANSLATE_GTX_FALLBACK": "0",
            "MAGI_FILE_TRANSLATE_CHECKPOINT_ENABLE": "1",
            "MAGI_FILE_TRANSLATE_CHECKPOINT_THRESHOLD_CHUNKS": "1",
            "MAGI_FILE_TRANSLATE_RETRIES": "0",
        }
        with patch.dict("os.environ", env, clear=False):
            result1 = orc._translate_text_complete(long_text)

        assert result1["success"] is True
        assert result1["chunks_failed"] == 0
        assert call_count[0] >= 2

        mock_gw.chat.reset_mock()
        mock_gw.chat.side_effect = AssertionError("checkpoint should prevent rerun")
        with patch.dict("os.environ", env, clear=False):
            result2 = orc._translate_text_complete(long_text)

        assert result2["success"] is True
        assert result2["text"] == result1["text"]
        mock_gw.chat.assert_not_called()

    @patch("api.handlers.translation_handler.InferenceGateway")
    @patch("skills.bridge.melchior_client.get_circuit_breaker_status", return_value={"open": False})
    def test_checkpoint_v2_bilingual_without_plain_translation_rebuilds_translated_text(self, mock_cb, mock_gw_cls, tmp_path):
        mock_gw = MagicMock()
        mock_gw.chat.side_effect = AssertionError("cached chunk results should prevent rerun")
        mock_gw_cls.return_value = mock_gw

        orc = _make_orchestrator()
        long_text = "English legal text about jurisdiction. " * 40

        env = {
            "MAGI_DOC_RUN_ROOT": str(tmp_path),
            "MAGI_FILE_TRANSLATE_CHECKPOINT_ENABLE": "1",
            "MAGI_FILE_TRANSLATE_CHECKPOINT_THRESHOLD_CHUNKS": "1",
            "MAGI_FILE_TRANSLATE_GTX_FALLBACK": "0",
        }
        with patch.dict("os.environ", env, clear=False):
            from api.handlers.translation_handler import _translation_checkpoint_state_path
            checkpoint_path = _translation_checkpoint_state_path(long_text, "auto", "繁體中文")
            checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
            checkpoint_path.write_text(json.dumps({
                "version": 2,
                "source_lang": "auto",
                "target_lang": "繁體中文",
                "chunks_total": 1,
                "chunks_failed": 0,
                "model": "google_gtx",
                "complete": True,
                "final_text": "【中英對照表】\n\n| 原文 | 中文 |\n| --- | --- |\n| A | B |",
                "results": [
                    {"text": "這是重建後的純中文譯文。", "model": "google_gtx", "failed": 0, "timed_out": False}
                ],
            }, ensure_ascii=False), encoding="utf-8")

            result = orc._translate_text_complete(long_text)

        assert result["success"] is True
        assert "【中英對照表】" in result["text"]
        assert result["translated_text"] == "這是重建後的純中文譯文。"
        mock_gw.chat.assert_not_called()


class TestTranslateVerifyStep:
    """Semantic verification via taide-12b after translation."""

    @patch("api.handlers.translation_handler.InferenceGateway")
    @patch("skills.bridge.melchior_client.get_circuit_breaker_status", return_value={"open": False})
    def test_verify_pass_keeps_translation(self, mock_cb, mock_gw_cls):
        """When verify says '正確', original translation is kept."""
        call_idx = [0]

        def _mock_chat(prompt, **kwargs):
            call_idx[0] += 1
            task = kwargs.get("task_type", "")
            if task == "tc_review":
                return _make_gateway_response("正確", model="taide-12b")
            return _make_gateway_response("高品質翻譯結果")

        mock_gw = MagicMock()
        mock_gw.chat.side_effect = _mock_chat
        mock_gw_cls.return_value = mock_gw

        orc = _make_orchestrator()
        result = orc._translate_text_complete("Short English text.")
        assert result["success"] is True
        assert "高品質翻譯結果" in result["text"]

    @patch("api.handlers.translation_handler.InferenceGateway")
    @patch("skills.bridge.melchior_client.get_circuit_breaker_status", return_value={"open": False})
    def test_large_doc_verifies_only_sampled_chunks(self, mock_cb, mock_gw_cls):
        calls = {"translate": 0, "tc_review": 0}

        def _mock_chat(prompt, **kwargs):
            task = kwargs.get("task_type", "")
            if task == "tc_review":
                calls["tc_review"] += 1
                return _make_gateway_response("正確", model="taide-12b")
            calls["translate"] += 1
            return _make_gateway_response("抽樣驗證翻譯結果")

        mock_gw = MagicMock()
        mock_gw.chat.side_effect = _mock_chat
        mock_gw_cls.return_value = mock_gw

        orc = _make_orchestrator()
        long_text = ("This is a long English legal paragraph. " * 120 + "\n\n") * 6

        with patch.dict("os.environ", {
            "MAGI_FILE_TRANSLATE_GTX_FALLBACK": "0",
            "MAGI_FILE_TRANSLATE_VERIFY_MAX_CHUNKS": "2",
        }):
            result = orc._translate_text_complete(long_text, target_lang="繁體中文")

        assert result["success"] is True
        assert result["chunks_total"] >= 3
        assert calls["tc_review"] < result["chunks_total"]
        assert "| 原文 | 中文 |" in result["text"]
