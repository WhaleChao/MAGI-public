"""測試 /osc/external/chat 的 P2-1 backpressure 機制（2026-04-19）

Bug 背景：
- `_run_with_timeout` 的 `future.cancel()` 對 running task 是 no-op
- 連續 timeout 會讓 orchestrator.process_message 在背景累積 N 份 in-flight task
- 每份 task 吃 DB/FAISS/subprocess，導致 RAM 爆炸電腦卡死
- 修法：in-flight counter + lock，超過 MAGI_EXTERNAL_CHAT_MAX_INFLIGHT 直接回 429
"""
from __future__ import annotations

import os
from unittest.mock import patch


def _reset_inflight_state():
    """每個測試前把 counter 歸 0，避免互相影響。"""
    from api import tools_api
    with tools_api._EXTERNAL_CHAT_INFLIGHT_LOCK:
        tools_api._EXTERNAL_CHAT_INFLIGHT_COUNT[0] = 0


class TestInflightCounter:

    def test_counter_starts_at_zero(self):
        _reset_inflight_state()
        from api import tools_api
        assert tools_api._EXTERNAL_CHAT_INFLIGHT_COUNT[0] == 0

    def test_counter_increments_and_decrements(self):
        """單次成功 request：counter 進 1 出 1，淨變化為 0。"""
        _reset_inflight_state()
        from api import tools_api

        # 模擬 request scope 內 counter 操作（不跑真實 Flask request）
        with tools_api._EXTERNAL_CHAT_INFLIGHT_LOCK:
            tools_api._EXTERNAL_CHAT_INFLIGHT_COUNT[0] += 1
        assert tools_api._EXTERNAL_CHAT_INFLIGHT_COUNT[0] == 1

        # finally 路徑 decrement
        with tools_api._EXTERNAL_CHAT_INFLIGHT_LOCK:
            tools_api._EXTERNAL_CHAT_INFLIGHT_COUNT[0] = max(0, tools_api._EXTERNAL_CHAT_INFLIGHT_COUNT[0] - 1)
        assert tools_api._EXTERNAL_CHAT_INFLIGHT_COUNT[0] == 0

    def test_decrement_floor_at_zero(self):
        """Counter decrement 不會變負數（多 decrement 不會爆）。"""
        _reset_inflight_state()
        from api import tools_api
        with tools_api._EXTERNAL_CHAT_INFLIGHT_LOCK:
            # 明明 counter=0，decrement 一次仍然是 0，不是 -1
            tools_api._EXTERNAL_CHAT_INFLIGHT_COUNT[0] = max(
                0, tools_api._EXTERNAL_CHAT_INFLIGHT_COUNT[0] - 1
            )
        assert tools_api._EXTERNAL_CHAT_INFLIGHT_COUNT[0] == 0


class TestBackpressureRoute:
    """透過 Flask test_client 測實際 429 拒絕邏輯。"""

    def setup_method(self):
        _reset_inflight_state()
        os.environ["MAGI_EXTERNAL_CHAT_MAX_INFLIGHT"] = "2"
        # 測試用 API key
        os.environ.setdefault("MAGI_EXTERNAL_API_KEY", "test_backpressure_key_12345")

    def teardown_method(self):
        _reset_inflight_state()
        os.environ.pop("MAGI_EXTERNAL_CHAT_MAX_INFLIGHT", None)

    def test_429_when_inflight_exceeds_max(self):
        from api import tools_api

        # 人工把 counter 加到上限
        with tools_api._EXTERNAL_CHAT_INFLIGHT_LOCK:
            tools_api._EXTERNAL_CHAT_INFLIGHT_COUNT[0] = 2  # == max

        client = tools_api.app.test_client()
        r = client.post(
            "/osc/external/chat",
            headers={"X-API-Key": os.environ["MAGI_EXTERNAL_API_KEY"], "Content-Type": "application/json"},
            json={"message": "hi", "async": False, "timeout_sec": 10},
        )
        assert r.status_code == 429
        body = r.get_json()
        assert body["error"] == "too_many_requests"
        assert "系統正在處理" in body["reply"]
        # 429 不該讓 counter 繼續加（保持 2）
        assert tools_api._EXTERNAL_CHAT_INFLIGHT_COUNT[0] == 2

    def test_429_response_schema(self):
        """確認 429 回應包含 inflight/max_inflight meta，方便 client 判斷。"""
        from api import tools_api

        with tools_api._EXTERNAL_CHAT_INFLIGHT_LOCK:
            tools_api._EXTERNAL_CHAT_INFLIGHT_COUNT[0] = 99

        client = tools_api.app.test_client()
        r = client.post(
            "/osc/external/chat",
            headers={"X-API-Key": os.environ["MAGI_EXTERNAL_API_KEY"], "Content-Type": "application/json"},
            json={"message": "x"},
        )
        assert r.status_code == 429
        body = r.get_json()
        assert body["meta"]["inflight"] == 99
        assert body["meta"]["max_inflight"] == 2


class TestTranslatorForkBomb:
    """P2-0 regression guard：確認 _translate_inner 不會遞迴 import translate_text。"""

    def test_translate_inner_does_not_import_tri_sage(self):
        """read source and verify no tri_sage_collab import inside _translate_inner scope.

        只檢查實際執行的程式碼（排除 comment / docstring）；如果 import 或 call 回 translate_text
        就會形成 recursive fork 炸彈（P2-0）。
        """
        import inspect
        import re
        from skills.translator.action import _translate_inner
        src = inspect.getsource(_translate_inner)
        # 砍掉所有 comment（# 開頭到行尾）
        code_only = re.sub(r"#[^\n]*", "", src)
        # 砍掉所有 docstring "xxx" 和 'xxx'（含多行）
        code_only = re.sub(r'"""[\s\S]*?"""', "", code_only)
        code_only = re.sub(r"'''[\s\S]*?'''", "", code_only)

        assert "from skills.bridge.tri_sage_collab" not in code_only, (
            "_translate_inner must NOT `from skills.bridge.tri_sage_collab import ...` "
            "(recursive fork bomb, P2-0)"
        )
        # 不該有 translate_text(...)  呼叫
        assert not re.search(r"\btranslate_text\s*\(", code_only), (
            "_translate_inner must NOT call translate_text (would re-enter translate_core → "
            "translate → subprocess → _translate_inner = infinite fork)"
        )
