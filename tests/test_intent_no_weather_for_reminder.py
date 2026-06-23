"""
Regression test: Bug #3 — reminder-style prompts must NOT be classified as weather queries.
"""
import os
import sys
import unittest

_MAGI_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _MAGI_ROOT not in sys.path:
    sys.path.insert(0, _MAGI_ROOT)

from api.pipelines import message_pipeline
from api.pipelines.message_pipeline import (
    _looks_like_model_capability_query,
    _looks_like_tool_capability_query,
    _try_agentic_route,
    _try_semantic_preflight,
)
from skills.engine.realtime_data_gateway import classify_realtime_query


class _PreflightOrchestrator:
    def __init__(self):
        self.traces = []

    def _append_route_trace(self, *args, **kwargs):
        self.traces.append((args, kwargs))

    def get_active_heavy_tasks(self):
        return []

    def _brain_runtime_banner(self):
        return "Local test brain"

    def _try_conversational_intent(self, message, msg_lower, user_id, role, platform):
        return ""

    def _handle_chat_async(self, user_id, message, platform_hint="LINE"):
        return "⚠️ 目前模型忙碌中，請稍後再試一次。"


class TestNoWeatherForReminder(unittest.TestCase):
    """Reminder/schedule prompts should not be misrouted to weather intent."""

    REMINDER_PROMPTS = [
        "明天下午三點提醒我開會",
        "明天早上九點提醒我",
        "幫我記下來明天有會議",
        "今天下午兩點有個會議",
        "後天下午提醒我備忘",
    ]

    WEATHER_PROMPTS = [
        "今天台北天氣如何？",
        "明天會不會下雨",
        "台中氣溫幾度",
        "颱風來了嗎",
    ]

    POLITE_WEATHER_ACTION_PROMPTS = [
        "那你能查一下明天台北的天氣嗎",
        "你可以幫我查今天花蓮天氣嗎",
        "可以查一下台中氣溫幾度嗎",
    ]

    CAPABILITY_PROMPTS = [
        "你會追蹤股票嗎？",
        "你可以查天氣嗎？",
        "MAGI 能不能查匯率？",
    ]

    def test_reminder_prompts_not_weather(self):
        """Reminder-style prompts (含「提醒」「開會」「會議」「備忘」) must not classify as weather."""
        for prompt in self.REMINDER_PROMPTS:
            result = classify_realtime_query(prompt)
            self.assertNotEqual(
                result, "weather",
                f"Prompt '{prompt}' was incorrectly classified as weather (got: {result})"
            )

    def test_weather_prompts_still_classified(self):
        """Actual weather prompts should still be classified as weather."""
        for prompt in self.WEATHER_PROMPTS:
            result = classify_realtime_query(prompt)
            self.assertEqual(
                result, "weather",
                f"Weather prompt '{prompt}' was NOT classified as weather (got: {result})"
            )

    def test_polite_weather_action_prompts_still_classified(self):
        """Polite action requests should not be mistaken for capability questions."""
        for prompt in self.POLITE_WEATHER_ACTION_PROMPTS:
            result = classify_realtime_query(prompt)
            self.assertEqual(
                result, "weather",
                f"Polite weather prompt '{prompt}' was NOT classified as weather (got: {result})"
            )

    def test_capability_prompts_stay_on_help_routes(self):
        """Capability questions should explain features instead of firing live data APIs."""
        for prompt in self.CAPABILITY_PROMPTS:
            result = classify_realtime_query(prompt)
            self.assertIsNone(
                result,
                f"Capability prompt '{prompt}' was incorrectly classified as realtime (got: {result})"
            )

    def test_compound_model_capability_query_uses_meta_route(self):
        """Mixed model/capability questions should be answered locally."""
        self.assertTrue(_looks_like_model_capability_query("你現在是什麼模型可以做什麼事情"))
        self.assertTrue(_looks_like_model_capability_query("有什麼功能"))
        self.assertFalse(_looks_like_model_capability_query("那你能查一下明天台北的天氣嗎"))

    def test_tool_capability_question_does_not_execute_weather(self):
        """Capability questions about tools should explain capability, not call realtime APIs."""
        self.assertTrue(_looks_like_tool_capability_query("你可以查天氣嗎？"))
        self.assertFalse(_looks_like_tool_capability_query("那你能查一下明天台北的天氣嗎"))

    def test_semantic_preflight_routes_meta_and_realtime_before_llm(self):
        """Clear meta/realtime prompts should not fall through to generic busy LLM replies."""
        original_get_brain_status = message_pipeline.get_brain_status
        message_pipeline.get_brain_status = lambda: "TEST_BRAIN_STATUS"
        try:
            meta = _try_semantic_preflight(_PreflightOrchestrator(), "你現在是什麼模型可以做什麼事情")
            self.assertIn("TEST_BRAIN_STATUS", meta)
            self.assertNotIn("功能總覽", meta)

            tool_cap = _try_semantic_preflight(_PreflightOrchestrator(), "你可以查天氣嗎？")
            self.assertIn("能力詢問", tool_cap)
            self.assertNotIn("忙碌", tool_cap)

            weather = _try_semantic_preflight(_PreflightOrchestrator(), "那你能查一下明天台北的天氣嗎")
            self.assertTrue("中央氣象署" in weather or "無法取得" in weather)
            self.assertNotIn("請稍後", weather)
        finally:
            message_pipeline.get_brain_status = original_get_brain_status

    def test_casual_chat_preflight_bypasses_busy_template(self):
        """Explicit casual chat should not be eaten by stale forms or busy templates."""
        orch = _PreflightOrchestrator()
        reply = _try_semantic_preflight(orch, "我只是想跟你聊聊天")
        self.assertIn("一般聊天", reply)
        self.assertIn("不會拿去填寫", reply)
        self.assertNotIn("請稍後", reply)
        self.assertTrue(any(args[2] == "semantic_preflight" and args[3] == "casual_chat" for args, _ in orch.traces))

    def test_agentic_route_uses_react_tools_for_broad_tasks(self):
        """Broad analysis/search prompts should reach the ReAct tool agent."""
        from skills.bridge import ensemble_inference
        from skills.bridge.ensemble_inference import ConsensusResult

        calls = []
        original_enabled = ensemble_inference._ENSEMBLE_TOOLS_ENABLED
        original_ensemble = ensemble_inference.ensemble_chat_with_tools
        original_format = ensemble_inference.format_magi_response

        def fake_ensemble_chat_with_tools(**kwargs):
            calls.append(kwargs)
            return ConsensusResult(
                unanimous=True,
                result="AGENT_ANSWER",
                individual_results={"tools_used": ["search_statutes"], "react_trace": {"steps": 1}},
                task_type="agentic",
            )

        try:
            ensemble_inference._ENSEMBLE_TOOLS_ENABLED = True
            ensemble_inference.ensemble_chat_with_tools = fake_ensemble_chat_with_tools
            ensemble_inference.format_magi_response = lambda cr: cr.result

            orch = _PreflightOrchestrator()
            reply = _try_agentic_route(orch, "請幫我比較民法184條與相關判決見解")
        finally:
            ensemble_inference._ENSEMBLE_TOOLS_ENABLED = original_enabled
            ensemble_inference.ensemble_chat_with_tools = original_ensemble
            ensemble_inference.format_magi_response = original_format

        self.assertEqual(reply, "AGENT_ANSWER")
        self.assertEqual(calls[0]["task_type"], "agentic")
        self.assertIn("完整 AI Agent", calls[0]["system"])
        self.assertTrue(any(args[2] == "agentic_router" and args[3] == "agent_task" for args, _ in orch.traces))

    def test_agentic_route_propagates_heavy_opt_in(self):
        """@heavy must reach the tool-capable ReAct route, not only generic chat."""
        from skills.bridge import ensemble_inference
        from skills.bridge.ensemble_inference import ConsensusResult

        calls = []
        original_enabled = ensemble_inference._ENSEMBLE_TOOLS_ENABLED
        original_ensemble = ensemble_inference.ensemble_chat_with_tools
        original_format = ensemble_inference.format_magi_response

        def fake_ensemble_chat_with_tools(**kwargs):
            calls.append(kwargs)
            return ConsensusResult(
                unanimous=True,
                result="HEAVY_AGENT_ANSWER",
                individual_results={
                    "tools_used": ["search_statutes"],
                    "react_trace": {"steps": 1, "llm_route": "nvidia_nim"},
                    "heavy": True,
                    "primary_route": "nvidia_nim",
                },
                task_type="agentic",
            )

        try:
            ensemble_inference._ENSEMBLE_TOOLS_ENABLED = True
            ensemble_inference.ensemble_chat_with_tools = fake_ensemble_chat_with_tools
            ensemble_inference.format_magi_response = lambda cr: cr.result

            orch = _PreflightOrchestrator()
            reply = _try_agentic_route(
                orch,
                "請幫我比較民法184條與相關判決見解",
                heavy=True,
            )
        finally:
            ensemble_inference._ENSEMBLE_TOOLS_ENABLED = original_enabled
            ensemble_inference.ensemble_chat_with_tools = original_ensemble
            ensemble_inference.format_magi_response = original_format

        self.assertEqual(reply, "HEAVY_AGENT_ANSWER")
        self.assertTrue(calls[0]["heavy"])
        trace_meta = orch.traces[0][0][4]
        self.assertTrue(trace_meta["heavy"])
        self.assertEqual(trace_meta["primary_route"], "nvidia_nim")

    def test_agentic_route_falls_back_to_tool_observations_on_internal_tags(self):
        """Internal model tags should not leak; ReAct observations should be synthesized instead."""
        from skills.bridge import ensemble_inference
        from skills.bridge.ensemble_inference import ConsensusResult

        original_enabled = ensemble_inference._ENSEMBLE_TOOLS_ENABLED
        original_ensemble = ensemble_inference.ensemble_chat_with_tools
        original_format = ensemble_inference.format_magi_response

        def fake_ensemble_chat_with_tools(**kwargs):
            return ConsensusResult(
                unanimous=True,
                result="<|channel>thought\n<channel|><|channel>thought\n<channel|>",
                individual_results={
                    "tools_used": ["search_statutes", "search_judgments"],
                    "react_trace": {
                        "steps": 2,
                        "trace": [
                            {
                                "type": "observation",
                                "content": "法規搜尋完成：民法 第 184 條\n民法\n第 184 條\n因故意或過失，不法侵害他人之權利者，負損害賠償責任。",
                            },
                            {
                                "type": "observation",
                                "content": "📚 判決搜尋完成：民法184 損害賠償\n引用連結: example",
                            },
                        ],
                    },
                },
                task_type="agentic",
            )

        try:
            ensemble_inference._ENSEMBLE_TOOLS_ENABLED = True
            ensemble_inference.ensemble_chat_with_tools = fake_ensemble_chat_with_tools
            ensemble_inference.format_magi_response = lambda cr: cr.result

            orch = _PreflightOrchestrator()
            reply = _try_agentic_route(orch, "請幫我比較民法184條與相關判決見解，整理成三點")
        finally:
            ensemble_inference._ENSEMBLE_TOOLS_ENABLED = original_enabled
            ensemble_inference.ensemble_chat_with_tools = original_ensemble
            ensemble_inference.format_magi_response = original_format

        self.assertIn("工具結果做保守整理", reply)
        self.assertIn("民法第184條", reply)
        self.assertIn("侵害他人權利", reply)
        self.assertIn("search_statutes、search_judgments", reply)
        self.assertNotIn("<|channel>", reply)

    def test_agentic_route_falls_back_on_empty_done_with_veto(self):
        """A bare done message plus reviewer veto is not a useful agent answer."""
        from skills.bridge import ensemble_inference
        from skills.bridge.ensemble_inference import ConsensusResult

        original_enabled = ensemble_inference._ENSEMBLE_TOOLS_ENABLED
        original_ensemble = ensemble_inference.ensemble_chat_with_tools
        original_format = ensemble_inference.format_magi_response

        def fake_ensemble_chat_with_tools(**kwargs):
            return ConsensusResult(
                unanimous=False,
                result="✅ 已完成處理。",
                vetoed_by=["phi4"],
                veto_reasons=["Melchior: 回覆超出了原始問題的範圍"],
                individual_results={
                    "tools_used": ["search_statutes", "search_judgments"],
                    "react_trace": {
                        "steps": 2,
                        "trace": [
                            {
                                "type": "observation",
                                "content": "法規搜尋完成：民法 第 184 條\n民法\n第 184 條\n因故意或過失，不法侵害他人之權利者，負損害賠償責任。",
                            },
                            {"type": "observation", "content": "📚 判決搜尋完成：民法184 損害賠償"},
                        ],
                    },
                },
                task_type="agentic",
            )

        try:
            ensemble_inference._ENSEMBLE_TOOLS_ENABLED = True
            ensemble_inference.ensemble_chat_with_tools = fake_ensemble_chat_with_tools
            ensemble_inference.format_magi_response = lambda cr: "✅ 已完成處理。\n\n─── 三哲人意見分歧 ───\n【Melchior】異議：回覆超出了原始問題的範圍"

            orch = _PreflightOrchestrator()
            reply = _try_agentic_route(orch, "請幫我比較民法184條與相關判決見解，整理成三點")
        finally:
            ensemble_inference._ENSEMBLE_TOOLS_ENABLED = original_enabled
            ensemble_inference.ensemble_chat_with_tools = original_ensemble
            ensemble_inference.format_magi_response = original_format

        self.assertIn("工具結果做保守整理", reply)
        self.assertIn("民法第184條", reply)
        self.assertNotIn("已完成處理", reply)


if __name__ == "__main__":
    unittest.main()
