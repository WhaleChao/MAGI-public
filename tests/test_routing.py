"""Tests for routing hardening and routing transparency."""

import json
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
        orc.classifier.classify_detailed.return_value = {
            "intent": "CHAT",
            "confidence": 0.55,
            "reason": "fixture_default",
            "candidates": [{"intent": "CHAT", "score": 0.55}],
        }
        orc.classifier.classify.return_value = "CHAT"
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
        assert result["requires_admin"] is False

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

    # ---- 4. OpenClaw update removed (2026-04-20, Phase 1 of cleanup plan) ----
    # OpenClaw Gateway chain was deleted; "更新openclaw" is no longer a
    # recognized admin command and falls through to the generic chat handler.
    def test_openclaw_update_removed_falls_through_to_chat(self):
        result = self.orc._explain_routing("更新openclaw")
        assert result["action"] != "openclaw_update"
        # Implementation detail: the non-matched path returns "chat_handler".
        # We assert the absence of the old route rather than pinning to a
        # specific fallthrough label, to stay robust to dispatcher changes.

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
        self.orc.classifier.classify_detailed.return_value = {
            "intent": "CMD",
            "confidence": 0.91,
            "reason": "llm_classifier",
            "candidates": [{"intent": "CMD", "score": 0.91}],
        }
        result = self.orc._explain_routing("請幫我關燈")
        assert result["action"] == "command_handler"
        assert result["matched"] == "intent_classifier"
        assert result["intent"] == "CMD"
        assert result["confidence"] == pytest.approx(0.91)
        assert result["reason"] == "llm_classifier"

    def test_classifier_fallback_chat(self):
        self.orc.classifier.classify_detailed.return_value = {
            "intent": "CHAT",
            "confidence": 0.62,
            "reason": "heuristic_fallback",
            "candidates": [{"intent": "CHAT", "score": 0.62}],
        }
        result = self.orc._explain_routing("你好嗎")
        assert result["action"] == "chat_handler"
        assert result["matched"] == "intent_classifier"
        assert result["reason"] == "heuristic_fallback"

    def test_classifier_fallback_query(self):
        self.orc.classifier.classify_detailed.return_value = {
            "intent": "QUERY",
            "confidence": 0.88,
            "reason": "embedding_high_confidence",
            "candidates": [{"intent": "QUERY", "score": 0.88}],
        }
        result = self.orc._explain_routing("台北天氣如何")
        assert result["action"] == "query_handler"
        assert result["intent"] == "QUERY"

    def test_classifier_fallback_danger(self):
        self.orc.classifier.classify_detailed.return_value = {
            "intent": "DANGER",
            "confidence": 1.0,
            "reason": "regex_danger",
            "candidates": [{"intent": "DANGER", "score": 1.0}],
        }
        result = self.orc._explain_routing("delete everything")
        assert result["action"] == "danger_handler"

    def test_classifier_exception_returns_unknown(self):
        self.orc.classifier.classify_detailed.side_effect = RuntimeError("boom")
        result = self.orc._explain_routing("random gibberish xyz")
        assert result["action"] == "unknown"
        assert result["intent"] == "UNKNOWN"

    # ---- Edge cases ----
    def test_empty_message_falls_to_classifier(self):
        self.orc.classifier.classify_detailed.return_value = {
            "intent": "CHAT",
            "confidence": 0.55,
            "reason": "heuristic_fallback",
            "candidates": [],
        }
        result = self.orc._explain_routing("")
        assert result["matched"] == "intent_classifier"

    def test_legacy_classifier_output_is_still_supported(self):
        self.orc.classifier.classify_detailed.return_value = "QUERY"
        self.orc.classifier.classify.return_value = "QUERY"

        result = self.orc._explain_routing("legacy classifier path")

        assert result["action"] == "query_handler"
        assert result["intent"] == "QUERY"
        assert result["reason"] == "legacy_classifier_fallback"

    def test_all_results_have_success_true(self):
        for msg in ["/help", "狀態", "行程", "翻譯 hi", "系統狀態"]:
            result = self.orc._explain_routing(msg)
            assert result["success"] is True


class TestRoutingHardening:
    @pytest.mark.parametrize("msg", ["摘要", "翻譯", "記得", "案件", "查詢", "搜尋", "開庭"])
    def test_generic_terms_do_not_hard_dispatch(self, msg):
        from skills.bridge.semantic_router import route

        result = route(msg)
        if result is not None:
            assert result["method"] != "phrase"

    @pytest.mark.parametrize(
        "msg,expected_skill",
        [
            ("法院判決全文", "run_judgment_collector"),
            ("幫我翻譯這份文件成英文", "tri_sage_translate"),
            ("今天開庭時間是什麼時候", "list_meetings"),
        ],
    )
    def test_specific_requests_still_route(self, msg, expected_skill):
        from skills.bridge.semantic_router import route

        result = route(msg)
        assert result is not None
        assert result["skill"] == expected_skill
        assert result["confidence"] >= 0.22
        assert result["candidates"]

    def test_persistent_cache_skips_high_risk_intents(self, tmp_path, monkeypatch):
        from skills.bridge import intention_classifier as ic

        cache_path = tmp_path / "intent_classifier_cache.json"
        monkeypatch.setattr(ic, "_CACHE_PERSIST_PATH", str(cache_path))
        monkeypatch.setattr(ic.IntentionClassifier, "_ask_llm", lambda self, text: "")

        clf = ic.IntentionClassifier(use_llm=False, cache_size=8)
        assert clf.classify("哈囉你好") == "CHAT"
        assert clf.classify("台北天氣如何") == "QUERY"
        assert clf.classify("幫我翻譯這份文件") in {"QUERY", "CMD"}

        payload = json.loads(cache_path.read_text(encoding="utf-8"))
        assert payload["schema_version"] == 2
        assert payload["policy_version"] == 2
        assert payload["model"]
        assert payload["use_llm"] is False
        assert payload["items"] == {"哈囉你好": "CHAT"}

    def test_old_cache_payload_is_ignored(self, tmp_path, monkeypatch):
        from skills.bridge import intention_classifier as ic

        cache_path = tmp_path / "intent_classifier_cache.json"
        cache_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "policy_version": 1,
                    "model": "legacy-model",
                    "use_llm": True,
                    "items": {"台北天氣如何": "CHAT"},
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr(ic, "_CACHE_PERSIST_PATH", str(cache_path))
        monkeypatch.setattr(ic.IntentionClassifier, "_ask_llm", lambda self, text: "")

        clf = ic.IntentionClassifier(use_llm=False, cache_size=8)
        assert clf._cache_get("台北天氣如何") is None
        assert clf.classify("台北天氣如何") == "QUERY"

    def test_soft_ambiguous_term_returns_none(self):
        from skills.bridge.semantic_router import route

        assert route("案件") is None
