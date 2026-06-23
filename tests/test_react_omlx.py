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
        # 常駐 11 個（含 search_judgments/search_statutes/run_skill，不含 remember）
        # 2026-04-20：加入 search_judgments + search_statutes 兩個法律直連工具
        self.assertEqual(len(tools), 11)
        for name in ["search_memory", "web_search", "query_cases", "get_schedule",
                      "calculate", "current_time", "summarize", "translate",
                      "search_judgments", "search_statutes", "run_skill"]:
            self.assertIn(name, tools, "{} should be in compact tools".format(name))
        self.assertNotIn("remember", tools)

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
        # 確認 tools 含 compact set（含 T5 加入的 run_skill）
        self.assertIn("current_time", engine.tools)
        self.assertIn("run_skill", engine.tools)

    def test_soul_text_injected(self):
        from skills.engine.react_engine import ReActEngine
        engine = ReActEngine.for_omlx(soul_text="我是 Casper")
        self.assertEqual(engine._soul_text, "我是 Casper")
        prompt = engine._build_system_prompt(soul_text="我是 Casper")
        self.assertTrue(prompt.startswith("我是 Casper"))

    def test_heavy_uses_nim_but_keeps_react_prompt(self):
        from skills.engine.react_engine import ReActEngine

        captured = []

        def fake_nim(**kwargs):
            captured.append(kwargs)
            return {
                "success": True,
                "response": "FINAL: heavy answer",
                "model": "nvidia/nemotron-3-super-120b-a12b",
                "pii_scrubbed": False,
                "pii_counts": {},
            }

        with patch("skills.bridge.nim_heavy.run_nim_chat", side_effect=fake_nim):
            engine = ReActEngine.for_omlx(tools={}, user_query="請分析", heavy=True)
            result = engine.run("請分析")

        self.assertEqual(engine._llm_route, "nvidia_nim")
        self.assertTrue(result["success"])
        self.assertEqual(result["answer"], "heavy answer")
        self.assertTrue(captured[0]["heavy"])
        self.assertEqual(captured[0]["task_type"], "agentic")
        self.assertIn("ACTION:", captured[0]["system_prompt"])
        self.assertIn("FINAL:", captured[0]["system_prompt"])

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

    def test_interpreter_empirical_request_forces_classifier_skill(self):
        from skills.engine.react_engine import ReActEngine

        calls = []
        tools = {
            "run_skill": {
                "fn": lambda **params: calls.append(params) or "TOOL_CALLED:run_skill",
                "desc": "run skill",
                "params": "",
            },
            "search_judgments": {
                "fn": lambda **params: "TOOL_CALLED:search_judgments",
                "desc": "search judgments",
                "params": "",
            },
        }
        llm = lambda messages: (
            'ACTION: search_judgments\n'
            'PARAMS: {"keywords":"最高法院 通譯","max_results":10}'
        )
        engine = ReActEngine(tools=tools, llm_fn=llm, max_steps=1)
        result = engine.run("請用關鍵字「最高法院 通譯」上網抓取裁判並產出通譯判決實證研究分類表。")

        self.assertIn("run_skill", result["tools_used"])
        self.assertEqual(calls[0]["skill_name"], "interpreter-empirical-classifier")
        self.assertEqual(calls[0]["task"], "fetch_and_classify")
        self.assertIn("最高法院 通譯", calls[0]["params"])


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

    @patch("skills.bridge.ensemble_inference._ENSEMBLE_TOOLS_ENABLED", True)
    def test_heavy_flag_reaches_react_engine(self):
        """ensemble_chat_with_tools(heavy=True) must use the NIM-backed ReAct route."""
        class FakeEngine:
            def run(self, prompt, context=""):
                return {
                    "success": True,
                    "answer": "heavy agent answer",
                    "trace": [{"type": "final", "content": "heavy agent answer"}],
                    "steps": 1,
                    "tools_used": [],
                    "elapsed_sec": 0,
                }

        with patch("skills.engine.react_engine.ReActEngine.for_omlx", return_value=FakeEngine()) as mock_for, \
             patch("skills.bridge.ensemble_inference._ensemble_review", return_value={}):
            from skills.bridge.ensemble_inference import ensemble_chat_with_tools
            result = ensemble_chat_with_tools(prompt="test", heavy=True)

        self.assertTrue(mock_for.call_args.kwargs["heavy"])
        self.assertEqual(result.individual_results["primary_route"], "nvidia_nim")
        self.assertTrue(result.individual_results["heavy"])


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


class TestSearchJudgmentsAndStatutes(unittest.TestCase):
    """新增直連工具 search_judgments / search_statutes 單元測試。"""

    def setUp(self):
        from skills.engine.tool_registry import TOOLS
        self.tools = TOOLS

    def test_search_judgments_in_registry(self):
        self.assertIn("search_judgments", self.tools)
        self.assertIn("fn", self.tools["search_judgments"])
        self.assertIn("keywords", self.tools["search_judgments"]["params"])

    def test_search_statutes_in_registry(self):
        self.assertIn("search_statutes", self.tools)
        self.assertIn("fn", self.tools["search_statutes"])
        self.assertIn("query", self.tools["search_statutes"]["params"])

    def test_search_judgments_requires_keywords(self):
        fn = self.tools["search_judgments"]["fn"]
        result = fn(keywords="")
        self.assertIn("關鍵字", result)

    def test_search_statutes_requires_query(self):
        fn = self.tools["search_statutes"]["fn"]
        result = fn(query="")
        self.assertIn("關鍵字", result)

    def test_search_statutes_exact_article_uses_local_vdb(self):
        from pathlib import Path
        import skills.engine.tool_registry as tool_registry

        original_root = tool_registry.MAGI_ROOT
        tool_registry.MAGI_ROOT = Path(MAGI_ROOT)
        try:
            fn = self.tools["search_statutes"]["fn"]
            result = fn(query="民法184條")
            self.assertIn("民法", result)
            self.assertIn("第 184 條", result)
            self.assertIn("侵害他人之權利", result)
            self.assertNotIn("第 1225 條", result)
        finally:
            tool_registry.MAGI_ROOT = original_root

    def test_run_skill_wrong_name_blocked(self):
        from skills.engine.tool_registry import _run_skill
        result = _run_skill(skill_name="hacker_tool")
        self.assertIn("⛔", result)
        self.assertIn("hacker_tool", result)

    def test_run_skill_no_name(self):
        from skills.engine.tool_registry import _run_skill
        result = _run_skill(skill_name="")
        self.assertIn("可用技能", result)

    def test_run_skill_valid_name_calls_tools_api(self):
        """run_skill 白名單技能向正確 endpoint 發 POST（mock 回 404）。"""
        from skills.engine.tool_registry import _run_skill
        with patch.dict(os.environ, {"MAGI_EXTERNAL_API_KEY": "unit-test-key", "MAGI_API_KEY": "unit-test-key"}, clear=False), \
             patch("skills.bridge.http_pool.get_session") as mock_session_fn:
            mock_resp = MagicMock()
            mock_resp.status_code = 404
            mock_resp.text = "not found"
            session = MagicMock()
            session.post.return_value = mock_resp
            mock_session_fn.return_value = session

            result = _run_skill(skill_name="judicial-web-search", task="search",
                                params='{"keywords": "侵權行為"}')
            # 確認打到了正確 endpoint（/skills/run）
            args, kwargs = session.post.call_args
            self.assertIn("/skills/run", args[0])
            body = kwargs["json"]
            self.assertEqual(body["skill"], "judicial-web-search")
            self.assertEqual(body["task"], "search")
            self.assertEqual(body["keywords"], "侵權行為")
            self.assertEqual(kwargs["headers"]["X-API-Key"], "unit-test-key")

    def test_allowed_skills_are_all_real_directories(self):
        """白名單中的 skill 名稱必須對應 skills/ 下真實目錄。"""
        import os
        from skills.engine.tool_registry import _ALLOWED_SKILLS
        skills_root = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "skills",
        )
        missing = [s for s in _ALLOWED_SKILLS if not os.path.isdir(os.path.join(skills_root, s))]
        self.assertEqual(missing, [],
                         f"白名單中這些 skill 目錄不存在: {missing}")

    def test_judicial_web_search_falls_back_to_http_when_venv_missing(self):
        """Missing .venv_judicial should not make the judgment tool unusable."""
        import importlib.util
        import os
        from pathlib import Path

        action_path = Path(MAGI_ROOT) / "skills" / "judicial-web-search" / "action.py"
        spec = importlib.util.spec_from_file_location("judicial_web_search_action_test", str(action_path))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        original_http = module._search_http_impl
        original_exists = module.os.path.exists
        calls = []

        def fake_http(**kwargs):
            calls.append(kwargs)
            return {"success": True, "engine": "http_form", "results": []}

        def fake_exists(path):
            if str(path) == str(module.VENV_PY):
                return False
            return original_exists(path)

        try:
            module._search_http_impl = fake_http
            module.os.path.exists = fake_exists
            result = module._search_impl("民法184 損害賠償", max_results=1, timeout_sec=5)
        finally:
            module._search_http_impl = original_http
            module.os.path.exists = original_exists

        self.assertTrue(result["success"])
        self.assertEqual(result["engine"], "http_form")
        self.assertEqual(calls[0]["keywords"], "民法184 損害賠償")


if __name__ == "__main__":
    unittest.main()
