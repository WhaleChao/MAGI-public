"""Tests for Orchestrator._explain_routing() — pure routing transparency."""

import pytest
from unittest.mock import MagicMock, patch


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
        orc.classifier = MagicMock()
        return orc


class TestExplainRouting:
    """Verify _explain_routing maps messages to the correct action without side effects."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.orc = _make_orchestrator()

    # ---- 1. Help keywords ----
    @pytest.mark.parametrize("msg", ["/help", "help", "指令", "說明", "功能", "menu", "helps", "/start"])
    def test_help_keywords(self, msg):
        result = self.orc._explain_routing(msg)
        assert result["action"] == "help_menu"
        assert result["matched"] == "universal_help"
        assert result["requires_admin"] is True

    # ---- 2. Status keywords ----
    @pytest.mark.parametrize("msg", ["狀態", "status", "大腦", "brain", "運作狀態"])
    def test_status_keywords(self, msg):
        result = self.orc._explain_routing(msg)
        assert result["action"] == "status_report"
        assert result["matched"] == "status_keywords"
        assert result["requires_admin"] is False

    # ---- 3. Schedule keywords ----
    @pytest.mark.parametrize("msg", ["行程", "schedule", "會議", "本週", "今天", "明天"])
    def test_schedule_keywords(self, msg):
        result = self.orc._explain_routing(msg)
        assert result["action"] == "schedule_query"
        assert result["matched"] == "schedule_keywords"

    # ---- 4. OpenClaw update (admin) ----
    def test_openclaw_update(self):
        result = self.orc._explain_routing("更新openclaw")
        assert result["action"] == "openclaw_update"
        assert result["requires_admin"] is True

    # ---- 5. Memory write (admin) ----
    @pytest.mark.parametrize("msg", ["記住這件事", "remember this fact"])
    def test_memory_write_requires_admin(self, msg):
        result = self.orc._explain_routing(msg)
        assert result["action"] == "memory_write"
        assert result["requires_admin"] is True

    # ---- 6. Translate prefix ----
    def test_translate_prefix(self):
        result = self.orc._explain_routing("翻譯 這段文字")
        assert result["action"] == "translate"
        assert result["matched"] == "translate_prefix"
        assert result["requires_admin"] is False

    def test_translate_english_prefix(self):
        result = self.orc._explain_routing("translate hello world")
        assert result["action"] == "translate"

    # ---- 7. Music prefix ----
    @pytest.mark.parametrize("msg", ["製作音樂 一首爵士", "生成音樂 悲傷的歌"])
    def test_music_prefix(self, msg):
        result = self.orc._explain_routing(msg)
        assert result["action"] == "music_generate"
        assert result["matched"] == "music_prefix"

    # ---- 8. Code analysis (admin) ----
    @pytest.mark.parametrize("msg", ["analyze code", "讀取程式碼"])
    def test_code_analysis_requires_admin(self, msg):
        result = self.orc._explain_routing(msg)
        assert result["action"] == "code_analysis_async"
        assert result["requires_admin"] is True

    # ---- 9. System monitor (admin) ----
    @pytest.mark.parametrize("msg", ["cpu", "ram", "健康檢查"])
    def test_system_monitor_requires_admin(self, msg):
        result = self.orc._explain_routing(msg)
        assert result["action"] == "system_monitor"
        assert result["requires_admin"] is True

    # ---- 10. Classifier fallback ----
    def test_classifier_fallback_cmd(self):
        self.orc.classifier.classify.return_value = "CMD"
        result = self.orc._explain_routing("請幫我關燈")
        assert result["action"] == "command_handler"
        assert result["matched"] == "intent_classifier"
        assert result["intent"] == "CMD"

    def test_classifier_fallback_chat(self):
        self.orc.classifier.classify.return_value = "CHAT"
        result = self.orc._explain_routing("你好嗎")
        assert result["action"] == "chat_handler"
        assert result["matched"] == "intent_classifier"

    def test_classifier_fallback_query(self):
        self.orc.classifier.classify.return_value = "QUERY"
        result = self.orc._explain_routing("台北天氣如何")
        assert result["action"] == "query_handler"
        assert result["intent"] == "QUERY"

    def test_classifier_fallback_danger(self):
        self.orc.classifier.classify.return_value = "DANGER"
        result = self.orc._explain_routing("delete everything")
        assert result["action"] == "danger_handler"

    def test_classifier_exception_returns_unknown(self):
        self.orc.classifier.classify.side_effect = RuntimeError("boom")
        result = self.orc._explain_routing("random gibberish xyz")
        assert result["action"] == "unknown"
        assert result["intent"] == "UNKNOWN"

    # ---- Edge cases ----
    def test_empty_message_falls_to_classifier(self):
        self.orc.classifier.classify.return_value = "CHAT"
        result = self.orc._explain_routing("")
        assert result["matched"] == "intent_classifier"

    def test_all_results_have_success_true(self):
        for msg in ["/help", "狀態", "行程", "翻譯 hi", "系統狀態"]:
            result = self.orc._explain_routing(msg)
            assert result["success"] is True
