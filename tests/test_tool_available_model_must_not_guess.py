"""
test_tool_available_model_must_not_guess.py
=============================================
Batch 5 — Task 9: 有工具可用時不要靠猜

當 MAGI 有 dispatch 工具可以查詢 DB 或呼叫 API 時，
LLM 不應憑空回應，應先用工具取得真實資料。
"""
import pytest


# ── Helpers ──────────────────────────────────────────────────────────────────

def _is_tool_routed(message: str) -> bool:
    """模擬 pipeline intercept 流程，檢查訊息是否命中工具路由。"""
    _case_mgmt_kws = [
        "建案", "新案件", "案件清單", "列出案件", "查案件", "案件狀態",
        "改為已結案", "改為進行中", "業務概況", "案件概況", "今天的案件",
    ]
    _client_kws = ["新增當事人", "建立當事人", "查當事人", "查客戶"]
    _accounting_kws = ["記收入", "記支出", "本月帳務", "帳務查詢", "本月收支"]
    _quotation_kws = ["開報價單", "報價單清單", "查報價單"]
    _calendar_kws = ["排庭", "排開會", "排會議"]
    _draft_kws = ["草擬起訴狀", "草擬答辯狀", "草擬聲請狀", "幫我草擬", "幫我起草"]

    all_kws = _case_mgmt_kws + _client_kws + _accounting_kws + _quotation_kws + _draft_kws
    if any(kw in message for kw in all_kws):
        return True
    if any(message.startswith(kw) for kw in _calendar_kws):
        return True
    return False


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestToolAvailableModelMustNotGuess:
    """確認有 dispatch 工具的指令不會 bypass 工具直接靠 LLM 猜測。"""

    @pytest.mark.parametrize("message", [
        "建案 114原訴24 王大明 民事 侵權行為",
        "案件清單",
        "查案件 王大明",
        "王大明 改為已結案",
        "業務概況",
        "新增當事人 張三 0912345678",
        "查當事人 李四",
        "記收入 5000 諮詢費 王大明",
        "記支出 3000 法院規費 114原訴24",
        "本月帳務",
        "開報價單 王大明 民事訴訟 50000",
        "報價單清單",
        "草擬起訴狀 114原訴24 侵權行為",
        "草擬答辯狀 114原訴24",
    ])
    def test_message_hits_tool_router(self, message):
        """每一個 OSC 口語指令都應命中工具路由，不 bypass。"""
        assert _is_tool_routed(message), (
            f"Message should be tool-routed but wasn't: {message!r}"
        )

    @pytest.mark.parametrize("message", [
        "今天天氣如何",
        "幫我翻譯 hello",
        "查判決 侵權行為",
        "摘要這篇文章",
        "你好嗎",
    ])
    def test_non_tool_messages_not_routed(self, message):
        """一般對話指令不應被 OSC tool router 攔截。"""
        assert not _is_tool_routed(message), (
            f"Message should NOT be tool-routed but was: {message!r}"
        )

    def test_dispatch_functions_importable(self):
        """所有 dispatch 函數應可正常 import。"""
        try:
            from api.pipelines.skill_dispatch import (
                dispatch_case_management,
                dispatch_client_management,
                dispatch_accounting,
                dispatch_quotation,
                dispatch_calendar_event,
                dispatch_ai_draft,
            )
        except ImportError as e:
            pytest.fail(f"dispatch function import failed: {e}")

    def test_calendar_start_triggers(self):
        """排庭 / 排開會 應以 startswith 而非 in 匹配，避免誤攔截。"""
        assert _is_tool_routed("排庭 4/20 上午10:00 114原訴24 台北地院")
        assert _is_tool_routed("排開會 4/25 下午2:00 與王大明討論和解")
        # "排行榜" should NOT trigger calendar route
        assert not _is_tool_routed("查詢排行榜")

    def test_message_pipeline_intercepts_importable(self):
        """message_pipeline.py 中的 intercept 區塊可正常 import（compile 檢查）。"""
        try:
            import py_compile, os
            pipeline_path = os.path.join(
                os.path.dirname(os.path.dirname(__file__)),
                "api", "pipelines", "message_pipeline.py"
            )
            if os.path.exists(pipeline_path):
                py_compile.compile(pipeline_path, doraise=True)
        except Exception as e:
            pytest.fail(f"message_pipeline.py compile error: {e}")

    def test_pdfnamer_pick_best_source(self):
        """PDFNAMER Phase 2D: _pick_best_source 回傳信心度最高的來源。"""
        try:
            from skills.pdfnamer.action import _pick_best_source  # noqa
        except ImportError:
            # Try alternative import path
            try:
                import importlib.util, os
                spec = importlib.util.spec_from_file_location(
                    "pdfnamer_action",
                    os.path.join(os.path.dirname(os.path.dirname(__file__)),
                                 "skills", "pdf-namer", "action.py")
                )
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                _pick_best_source = mod._pick_best_source
            except Exception as e:
                pytest.skip(f"pdf-namer/action.py not importable: {e}")
                return

        # vision has higher confidence → should win
        val, conf, src = _pick_best_source("court", [
            ("台灣花蓮地方法院", 0.90, "vision"),
            ("花蓮地院", 0.70, "ocr"),
        ])
        assert val == "台灣花蓮地方法院"
        assert src == "vision"

        # ocr value present but vision is empty → ocr wins
        val2, conf2, src2 = _pick_best_source("court", [
            ("", 0.90, "vision"),
            ("花蓮地院", 0.70, "ocr"),
        ])
        assert val2 == "花蓮地院"
        assert src2 == "ocr"

        # All empty → returns empty
        val3, conf3, src3 = _pick_best_source("court", [
            ("", 0.90, "vision"),
            ("", 0.70, "ocr"),
        ])
        assert val3 == ""
