"""
Tier 1 單元測試 — ReAct oMLX 整合 + ensemble_chat_with_tools
================================================================
mock LLM，無網路依賴。
"""
from __future__ import annotations

import json
import os
import sys
import unittest
from unittest.mock import patch, MagicMock

# 確保 MAGI root 在 path
MAGI_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if MAGI_ROOT not in sys.path:
    sys.path.insert(0, MAGI_ROOT)


class TestCompactTools(unittest.TestCase):
    """Phase 2: get_compact_tools 工具集。"""

    def test_always_tools_count(self):
        from skills.engine.tool_registry import get_compact_tools
        tools = get_compact_tools("")
        # 常駐 8 個（不含 remember）
        self.assertEqual(len(tools), 8)
        for name in ["search_memory", "web_search", "query_cases", "get_schedule",
                      "calculate", "current_time", "summarize", "translate"]:
            self.assertIn(name, tools, "{} should be in compact tools".format(name))
        self.assertNotIn("remember", tools)
        self.assertNotIn("run_skill", tools)

    def test_remember_gate_opens(self):
        from skills.engine.tool_registry import get_compact_tools
        for kw in ["請記住這件事", "幫我記一下", "記下來", "存起來", "備忘"]:
            tools = get_compact_tools(kw)
            self.assertIn("remember", tools, "remember should open for '{}'".format(kw))

    def test_remember_gate_closed(self):
        from skills.engine.tool_registry import get_compact_tools
        for kw in ["查案號", "現在幾點", "侵權行為"]:
            tools = get_compact_tools(kw)
            self.assertNotIn("remember", tools, "remember should NOT open for '{}'".format(kw))

    def test_total_desc_length(self):
        from skills.engine.tool_registry import get_compact_tools
        tools = get_compact_tools("")
        total = sum(len(v.get("desc", "")) + len(v.get("params", "")) for v in tools.values())
        self.assertLess(total, 2000, "Total tool description should be < 2000 chars, got {}".format(total))


class TestOmlxMultiturn(unittest.TestCase):
    """Phase 1: _call_omlx_chat_multiturn 格式。"""

    @patch("requests.post")
    @patch("requests.get")
    def test_sends_full_messages(self, mock_get, mock_post):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "choices": [{"message": {"content": "OK"}}]
        }
        mock_resp.raise_for_status = MagicMock()
        mock_post.return_value = mock_resp
        mock_get.side_effect = Exception("skip model probe")

        from skills.bridge.ensemble_inference import _call_omlx_chat_multiturn
        messages = [
            {"role": "system", "content": "你是助理"},
            {"role": "user", "content": "問題1"},
            {"role": "assistant", "content": "ACTION: current_time\nPARAMS: {}"},
            {"role": "user", "content": "OBSERVATION: 2026-04-14 15:30"},
        ]
        result = _call_omlx_chat_multiturn("http://fake:8080", "e4b", messages)
        self.assertTrue(result["success"])

        # 確認 payload 含完整 messages（不只 system+user）
        call_args = mock_post.call_args
        payload = call_args[1]["json"] if "json" in call_args[1] else call_args[0][1]
        self.assertEqual(len(payload["messages"]), 4)


class TestReActForOmlx(unittest.TestCase):
    """Phase 3: ReActEngine.for_omlx() 建構。"""

    def test_creates_engine(self):
        from skills.engine.react_engine import ReActEngine
        engine = ReActEngine.for_omlx(user_query="現在幾點")
        self.assertEqual(engine.max_steps, 5)
        self.assertEqual(engine.total_timeout, 60)
        self.assertIsNotNone(engine._llm)
        # 確認 tools 含 compact set
        self.assertIn("current_time", engine.tools)
        self.assertNotIn("run_skill", engine.tools)

    def test_soul_text_injected(self):
        from skills.engine.react_engine import ReActEngine
        engine = ReActEngine.for_omlx(soul_text="我是 Casper")
        self.assertEqual(engine._soul_text, "我是 Casper")
        prompt = engine._build_system_prompt(soul_text="我是 Casper")
        self.assertTrue(prompt.startswith("我是 Casper"))

    def test_react_action_parsing(self):
        from skills.engine.react_engine import ReActEngine
        engine = ReActEngine.for_omlx()
        # E4B 風格回覆（含 think 標籤）
        response = '<think>我需要查時間</think>\nACTION: current_time\nPARAMS: {}'
        tool, params = engine._parse_action(response)
        self.assertEqual(tool, "current_time")
        self.assertEqual(params, {})

    def test_react_final_parsing(self):
        from skills.engine.react_engine import ReActEngine
        engine = ReActEngine.for_omlx()
        response = "FINAL: 現在是下午三點三十分"
        answer = engine._parse_final(response)
        self.assertEqual(answer, "現在是下午三點三十分")

    def test_iron_dome_blocks(self):
        from skills.engine.react_engine import ReActEngine
        engine = ReActEngine.for_omlx()
        result = engine._iron_dome_check("read_file", {"path": "/etc/passwd; rm -rf /"})
        self.assertIsNotNone(result)
        self.assertIn("rm -rf", result)


class TestEnsembleChatWithTools(unittest.TestCase):
    """Phase 4: ensemble_chat_with_tools 入口。"""

    @patch("skills.bridge.ensemble_inference._ENSEMBLE_TOOLS_ENABLED", False)
    def test_flag_off_fallback(self):
        """Flag=0 時直接走 ensemble_chat_verified。"""
        with patch("skills.bridge.ensemble_inference.ensemble_chat_verified") as mock_ecv:
            from skills.bridge.ensemble_inference import ConsensusResult
            mock_ecv.return_value = ConsensusResult(unanimous=True, result="test", task_type="chat")
            from skills.bridge.ensemble_inference import ensemble_chat_with_tools
            result = ensemble_chat_with_tools(prompt="test")
            mock_ecv.assert_called_once()
            self.assertTrue(result.unanimous)

    @patch("skills.bridge.ensemble_inference._ENSEMBLE_TOOLS_ENABLED", True)
    def test_react_failure_fallback(self):
        """ReAct 失敗時 fallback 到 ensemble_chat_verified。"""
        with patch("skills.bridge.ensemble_inference.ensemble_chat_verified") as mock_ecv, \
             patch("skills.engine.react_engine.ReActEngine.for_omlx") as mock_for:
            from skills.bridge.ensemble_inference import ConsensusResult
            mock_for.side_effect = Exception("oMLX down")
            mock_ecv.return_value = ConsensusResult(unanimous=True, result="fallback", task_type="chat")
            from skills.bridge.ensemble_inference import ensemble_chat_with_tools
            result = ensemble_chat_with_tools(prompt="test")
            mock_ecv.assert_called_once()
            self.assertEqual(result.result, "fallback")


class TestFormatMagiResponseToolSource(unittest.TestCase):
    """Phase 6: format_magi_response 工具來源標註。"""

    def test_unanimous_with_tools(self):
        from skills.bridge.ensemble_inference import ConsensusResult, format_magi_response
        cr = ConsensusResult(
            unanimous=True, result="現在是下午三點",
            individual_results={"tools_used": ["current_time"]},
            task_type="chat",
        )
        text = format_magi_response(cr)
        self.assertIn("參考資料來源", text)
        self.assertIn("current_time", text)

    def test_unanimous_no_tools(self):
        from skills.bridge.ensemble_inference import ConsensusResult, format_magi_response
        cr = ConsensusResult(
            unanimous=True, result="正當防衛是...",
            individual_results={},
            task_type="chat",
        )
        text = format_magi_response(cr)
        self.assertNotIn("參考資料來源", text)

    def test_tools_dedup(self):
        from skills.bridge.ensemble_inference import ConsensusResult, format_magi_response
        cr = ConsensusResult(
            unanimous=True, result="答案",
            individual_results={"tools_used": ["web_search", "web_search", "summarize"]},
            task_type="chat",
        )
        text = format_magi_response(cr)
        # web_search 只出現一次
        self.assertEqual(text.count("web_search"), 1)


if __name__ == "__main__":
    unittest.main()
