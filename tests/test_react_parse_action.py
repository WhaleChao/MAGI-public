"""
tests/test_react_parse_action.py
=================================
ReActEngine._parse_action 修復驗證測試

涵蓋：
- _extract_balanced_json：正確處理巢狀 JSON
- _fix_json_lite：尾逗號移除，不破壞巢狀物件
- _parse_action：各種 LLM 輸出格式解析
- 功能旗標 MAGI_ENSEMBLE_TOOLS 預設值
"""
from __future__ import annotations

import json
import os
import sys
import unittest

MAGI_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if MAGI_ROOT not in sys.path:
    sys.path.insert(0, MAGI_ROOT)


class TestExtractBalancedJson(unittest.TestCase):
    """_extract_balanced_json 巢狀 JSON 提取。"""

    def setUp(self):
        from skills.engine.react_engine import ReActEngine
        self._extract = ReActEngine._extract_balanced_json

    def test_simple_flat_object(self):
        raw = '{"query": "侵權行為"}'
        result = self._extract(raw)
        self.assertEqual(json.loads(result), {"query": "侵權行為"})

    def test_nested_object(self):
        raw = '{"filter": {"type": "case", "year": 114}}'
        result = self._extract(raw)
        self.assertEqual(json.loads(result), {"filter": {"type": "case", "year": 114}})

    def test_deeply_nested(self):
        raw = '{"a": {"b": {"c": "d"}}}'
        result = self._extract(raw)
        self.assertEqual(json.loads(result), {"a": {"b": {"c": "d"}}})

    def test_braces_in_string_value(self):
        raw = '{"text": "rule {A} or {B}"}'
        result = self._extract(raw)
        self.assertEqual(json.loads(result), {"text": "rule {A} or {B}"})

    def test_array_value(self):
        raw = '{"ids": [1, 2, 3]}'
        result = self._extract(raw)
        self.assertEqual(json.loads(result), {"ids": [1, 2, 3]})

    def test_empty_object(self):
        raw = '{}'
        result = self._extract(raw)
        self.assertEqual(result, '{}')

    def test_leading_text_ignored(self):
        raw = 'ACTION: web_search\nPARAMS: {"q": "臺灣法律"}'
        result = self._extract(raw[raw.find('{'):])
        self.assertEqual(json.loads(result), {"q": "臺灣法律"})

    def test_no_brace_returns_none(self):
        result = self._extract("no JSON here")
        self.assertIsNone(result)

    def test_unclosed_brace_returns_none(self):
        result = self._extract('{"key": "value"')
        self.assertIsNone(result)


class TestFixJsonLite(unittest.TestCase):
    """_fix_json_lite 修復函式。"""

    def setUp(self):
        from skills.engine.react_engine import ReActEngine
        self._fix = ReActEngine._fix_json_lite

    def test_trailing_comma_object(self):
        raw = '{"a": 1,}'
        fixed = self._fix(raw)
        self.assertEqual(json.loads(fixed), {"a": 1})

    def test_trailing_comma_array(self):
        raw = '{"ids": [1, 2, 3,]}'
        fixed = self._fix(raw)
        self.assertEqual(json.loads(fixed), {"ids": [1, 2, 3]})

    def test_single_quote_to_double(self):
        raw = "{'key': 'value'}"
        fixed = self._fix(raw)
        self.assertEqual(json.loads(fixed), {"key": "value"})

    def test_nested_not_destroyed(self):
        """rstrip(",}") の古いバグ再現テスト — 巢狀が壊れないこと。"""
        raw = '{"outer": {"inner": "value"}}'
        fixed = self._fix(raw)
        self.assertEqual(json.loads(fixed), {"outer": {"inner": "value"}})

    def test_clean_json_unchanged(self):
        raw = '{"query": "民法第184條"}'
        fixed = self._fix(raw)
        self.assertEqual(json.loads(fixed), {"query": "民法第184條"})


class TestParseAction(unittest.TestCase):
    """_parse_action 各種 LLM 輸出格式解析。"""

    def setUp(self):
        from skills.engine.react_engine import ReActEngine
        self.engine = ReActEngine.for_omlx()

    def test_blocks_destructive_user_input_before_llm(self):
        from skills.engine.react_engine import ReActEngine

        called = {"llm": False}

        def _llm(_messages):
            called["llm"] = True
            return "FINAL: should not run"

        engine = ReActEngine(tools={}, llm_fn=_llm)
        result = engine.run("shutdown -h now")

        self.assertFalse(called["llm"])
        self.assertFalse(result["success"])
        self.assertTrue(any(t.get("type") == "blocked" for t in result["trace"]))
        self.assertIn("不可", result["answer"])

    # ─── 基本格式 ───────────────────────────────────────────────────────────

    def test_standard_format(self):
        resp = 'ACTION: current_time\nPARAMS: {}'
        tool, params = self.engine._parse_action(resp)
        self.assertEqual(tool, "current_time")
        self.assertEqual(params, {})

    def test_standard_with_param(self):
        resp = 'ACTION: web_search\nPARAMS: {"q": "臺東天氣"}'
        tool, params = self.engine._parse_action(resp)
        self.assertEqual(tool, "web_search")
        self.assertEqual(params, {"q": "臺東天氣"})

    # ─── 巢狀 JSON（舊 regex 的死角）────────────────────────────────────────

    def test_nested_params(self):
        """關鍵修復：巢狀 JSON 不再被截斷。"""
        resp = 'ACTION: query_cases\nPARAMS: {"filter": {"year": 114, "type": "刑事"}}'
        tool, params = self.engine._parse_action(resp)
        self.assertEqual(tool, "query_cases")
        self.assertEqual(params["filter"]["year"], 114)
        self.assertEqual(params["filter"]["type"], "刑事")

    def test_deeply_nested_params(self):
        resp = 'ACTION: search_memory\nPARAMS: {"opts": {"boost": {"legal": true}}}'
        tool, params = self.engine._parse_action(resp)
        self.assertEqual(tool, "search_memory")
        self.assertEqual(params["opts"]["boost"]["legal"], True)

    # ─── 尾逗號（舊 rstrip(",}") 的死角）────────────────────────────────────

    def test_trailing_comma_in_params(self):
        """尾逗號修復 + 巢狀不被破壞。"""
        resp = 'ACTION: calculate\nPARAMS: {"expr": "1+1",}'
        tool, params = self.engine._parse_action(resp)
        self.assertEqual(tool, "calculate")
        self.assertEqual(params.get("expr"), "1+1")

    def test_nested_not_broken_by_fix(self):
        """之前 rstrip(",}") 會把 {"a": {"b": "c"}} 截斷，現在不應。"""
        resp = 'ACTION: summarize\nPARAMS: {"opts": {"lang": "zh-TW"}}'
        tool, params = self.engine._parse_action(resp)
        self.assertEqual(tool, "summarize")
        self.assertEqual(params.get("opts", {}).get("lang"), "zh-TW")

    # ─── think 標籤（E4B 常見格式）──────────────────────────────────────────

    def test_think_tag_prefix(self):
        resp = '<think>讓我查一下時間</think>\nACTION: current_time\nPARAMS: {}'
        tool, params = self.engine._parse_action(resp)
        self.assertEqual(tool, "current_time")

    def test_think_tag_with_params(self):
        resp = '<think>需要搜尋</think>\nACTION: web_search\nPARAMS: {"q": "侵權行為判決"}'
        tool, params = self.engine._parse_action(resp)
        self.assertEqual(tool, "web_search")
        self.assertEqual(params["q"], "侵權行為判決")

    # ─── 無 PARAMS block，但有 JSON 物件在回覆中 ─────────────────────────────

    def test_no_params_block_json_in_response(self):
        """沒有 PARAMS: 前綴但有 JSON 時，嘗試從 response 整體提取。"""
        resp = 'ACTION: calculate\n{"expr": "3*7"}'
        tool, params = self.engine._parse_action(resp)
        self.assertEqual(tool, "calculate")
        self.assertEqual(params.get("expr"), "3*7")

    # ─── 錯誤容忍 ───────────────────────────────────────────────────────────

    def test_no_action_no_params(self):
        resp = "這只是普通回覆，沒有工具呼叫。"
        tool, params = self.engine._parse_action(resp)
        self.assertEqual(tool, "")
        self.assertEqual(params, {})

    def test_action_only_no_params(self):
        resp = "ACTION: current_time"
        tool, params = self.engine._parse_action(resp)
        self.assertEqual(tool, "current_time")
        self.assertEqual(params, {})

    def test_garbage_params_ignored(self):
        resp = "ACTION: web_search\nPARAMS: not_valid_json!@#"
        tool, params = self.engine._parse_action(resp)
        self.assertEqual(tool, "web_search")
        self.assertEqual(params, {})

    # ─── 多行 params ──────────────────────────────────────────────────────────

    def test_multiline_params(self):
        resp = (
            'ACTION: query_cases\n'
            'PARAMS: {\n'
            '  "case_no": "114年訴字第123號",\n'
            '  "court": "臺灣花蓮地方法院"\n'
            '}'
        )
        tool, params = self.engine._parse_action(resp)
        self.assertEqual(tool, "query_cases")
        self.assertEqual(params.get("case_no"), "114年訴字第123號")
        self.assertEqual(params.get("court"), "臺灣花蓮地方法院")


class TestEnsembleToolsFlagDefault(unittest.TestCase):
    """MAGI_ENSEMBLE_TOOLS 預設值應為 1（啟用）。"""

    def test_default_is_enabled(self):
        """新預設值為 '1'，不設環境變數時工具呼叫應啟用。"""
        import importlib
        import os
        # 確保沒有覆蓋環境變數干擾
        old = os.environ.pop("MAGI_ENSEMBLE_TOOLS", None)
        try:
            import skills.bridge.ensemble_inference as ei
            importlib.reload(ei)
            # 預設應為 True
            self.assertTrue(ei._ENSEMBLE_TOOLS_ENABLED)
        finally:
            if old is not None:
                os.environ["MAGI_ENSEMBLE_TOOLS"] = old
            # Restore to env-driven value
            import skills.bridge.ensemble_inference as ei2
            importlib.reload(ei2)

    def test_can_disable_with_env_var(self):
        """設定 MAGI_ENSEMBLE_TOOLS=0 可停用。"""
        import importlib
        import os
        os.environ["MAGI_ENSEMBLE_TOOLS"] = "0"
        try:
            import skills.bridge.ensemble_inference as ei
            importlib.reload(ei)
            self.assertFalse(ei._ENSEMBLE_TOOLS_ENABLED)
        finally:
            del os.environ["MAGI_ENSEMBLE_TOOLS"]
            import skills.bridge.ensemble_inference as ei2
            importlib.reload(ei2)


if __name__ == "__main__":
    unittest.main()
